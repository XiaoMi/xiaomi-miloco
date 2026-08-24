/**
 * 明细表:按**模型**一行(合并实时/用户两类调用),列出 调用次数 / 各模态 token / 费用估算。
 * 数据来自 backend omni 计费(cache/video/audio 均为 input 的子集)。
 *
 * 费用按行算而不是只给一个总数:单价是按模型存的,多模型时只有落到行上才对得起账。
 *
 * 口径说明从表下的一行小字改成标题旁的「?」——那句话是「看界面看不出来」的背景知识,
 * 需要时点开即可,常驻一行反而占位。
 */

import { Fragment } from "react";
import { useTranslation } from "react-i18next";
import type { TokenBreakdown, UsageStats } from "@/lib/types";
import { humanTokens } from "@/lib/formatTokens";
import { costInputOf, estimateCost, pricingFor, type UsagePricing } from "@/lib/usagePricing";
import { shortenUrlSet } from "@/lib/modelIdentity";
import { UsageUrlChip } from "./UsageUrlChip";
import { HelpTip } from "./HelpTip";

interface ModelRow {
  model: string;
  /** 完整 URL 原文；'' = 老数据未记录。与 model 一起构成唯一身份。 */
  base_url: string;
  calls: number;
  breakdown: TokenBreakdown;
}

/** 把 model×type 明细行按模型合并(累加调用数与各模态),模型名升序。 */
/**
 * 折成明细行。按 **(model, base_url)** 分组，不是只按 model——
 * 模型身份是这两者的组合，只按模型名分会把两个 endpoint 的用量合回一行，
 * 那就等于把后端刚拆开的东西又粘上，钱花在哪边仍然分不出来。
 */
function rowsByModel(stats: UsageStats): ModelRow[] {
  const byModel = new Map<string, ModelRow>();
  for (const r of stats.rows) {
    // 分隔符用 \u001f：模型名与 URL 都可能含空格
    const key = `${r.model}\u001f${r.base_url ?? ""}`;
    let m = byModel.get(key);
    if (!m) {
      m = {
        model: r.model,
        base_url: r.base_url ?? "",
        calls: 0,
        breakdown: { input: 0, output: 0, cache: 0, video: 0, audio: 0 },
      };
      byModel.set(key, m);
    }
    m.calls += r.calls;
    m.breakdown.input += r.breakdown.input;
    m.breakdown.output += r.breakdown.output;
    m.breakdown.cache += r.breakdown.cache;
    m.breakdown.video += r.breakdown.video;
    m.breakdown.audio += r.breakdown.audio;
  }
  return [...byModel.values()].sort((a, b) => {
    if (a.model !== b.model) return a.model < b.model ? -1 : 1;
    // 同模型名的多个 endpoint 也要有确定顺序，否则刷新时行序会跳
    return a.base_url < b.base_url ? -1 : a.base_url > b.base_url ? 1 : 0;
  });
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
  /**
   * 短形式对照**全表实际出现的 URL 集合**算，保证互不相同——固定规则逐个截时，
   * 差异落在预算之外的两行会截成同一串（见 shortenUrlSet 的说明）。
   * 22 是实测折中：够分辨、模型列约 +60px。
   */
  const urlLabels = shortenUrlSet(
    rows.map((r) => r.base_url).filter(Boolean),
    22,
  );
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
                  <Fragment key={`${r.model}\u001f${r.base_url}`}>
                  <tr className="border-b border-border last:border-b-0">
                    <td className="px-5 md:px-6 py-2.5 text-text-primary">
                      <span className="inline-flex items-center gap-1.5">
                        {r.model}
                        {/* 有记录值就显示它；空串是 schema v3 之前的老数据，
                            来源无从得知——直说，不做任何推断或回填。 */}
                        {r.base_url ? (
                          <UsageUrlChip
                            url={r.base_url}
                            label={urlLabels.get(r.base_url) ?? r.base_url}
                          />
                        ) : (
                          <span
                            className="text-text-tertiary text-[11px] px-1.5 py-px
                                       rounded border border-dashed border-border"
                            title={t("usage.modelUrlLegacyHint")}
                          >
                            {t("usage.modelUrlLegacy")}
                          </span>
                        )}
                      </span>
                    </td>
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
                  </Fragment>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
