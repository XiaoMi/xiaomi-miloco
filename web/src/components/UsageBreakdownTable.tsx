/**
 * 明细表:按**模型名 + endpoint** 一行(合并实时/用户两类调用),列出 调用次数 / 各模态
 * token。同一个模型名挂在两个 endpoint 上会分成两行。
 * 数据来自 backend omni 计数(cache/video/audio 均为 input 的子集)。
 *
 * 末列是「操作」：逐行清除该行「模型名 + endpoint」的用量。与卡片顶部那个清空共用
 * 同一个垃圾桶图标、同一套时间档位，只差作用域——顶部清所有模型，这里只清这一行。
 *
 * 口径说明从表下的一行小字改成标题旁的「?」——那句话是「看界面看不出来」的背景知识,
 * 需要时点开即可,常驻一行反而占位。
 */

import { Fragment } from "react";
import { useTranslation } from "react-i18next";
import type { TokenBreakdown, UsageStats } from "@/lib/types";
import { humanTokens } from "@/lib/formatTokens";
import { shortenUrlSet } from "@/lib/modelIdentity";
import { UsageUrlChip } from "./UsageUrlChip";
import { UsageClearMenu, type ClearScope } from "./UsageClearMenu";
import { HelpTip } from "./HelpTip";

interface ModelRow {
  model: string;
  /** 完整 URL 原文；'' = 老数据未记录。与 model 一起构成唯一身份。 */
  base_url: string;
  calls: number;
  breakdown: TokenBreakdown;
}

/**
 * 折成明细行。按 **(model, base_url)** 分组，不是只按 model——
 * 模型身份是这两者的组合，只按模型名分会把两个 endpoint 的用量合回一行，
 * 那就等于把后端刚拆开的东西又粘上，哪个 endpoint 用了多少就再也分不出来。
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
  onClear,
}: {
  stats: UsageStats;
  /** 给了才出「操作」列。逐行清除只清该行的「模型名 + endpoint」。 */
  onClear?: (s: ClearScope) => void;
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
  /**
   * 清除气泡标题里的作用域徽记：文字与框线跟模型列一致，但**不可交互**——
   * 气泡里再套一个能弹气泡的 chip 没有意义，完整 URL 挂 title 即可。
   */
  const scopeChipOf = (r: ModelRow) => (
    <>
      <span className="text-text-primary">{r.model}</span>
      {r.base_url ? (
        <span
          className="num text-text-tertiary text-[11px] px-1.5 py-px rounded border border-border"
          title={r.base_url}
        >
          {urlLabels.get(r.base_url) ?? r.base_url}
        </span>
      ) : (
        <span className="text-text-tertiary text-[11px] px-1.5 py-px rounded border border-dashed border-border">
          {t("usage.modelUrlLegacy")}
        </span>
      )}
    </>
  );

  const cols = [
    { key: "colModel", align: "left" as const },
    { key: "colCalls", align: "right" as const },
    { key: "colInput", align: "right" as const },
    { key: "colOutput", align: "right" as const },
    { key: "colCache", align: "right" as const },
    { key: "colCacheHitRate", align: "right" as const },
    { key: "colVideo", align: "right" as const },
    { key: "colAudio", align: "right" as const },
    ...(onClear ? [{ key: "colActions", align: "right" as const }] : []),
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
                  // 操作列给 width:1%：auto 布局按 max-content 分剩余宽度，不压住它
                  // 就会白拿一大块空隙，垃圾桶被推得离数字很远（实测 34px → 20px）。
                  style={c.key === "colActions" ? { width: "1%" } : undefined}
                  className={`py-2 font-normal ${
                    c.align === "left" ? "text-left" : "text-right num"
                  } ${
                    c.key === "colActions"
                      ? "pl-2 pr-5 md:pr-6"
                      : i === 0
                        ? "px-5 md:px-6"
                        : "px-3"
                  }`}
                >
                  {c.key === "colActions" ? (
                    // 一列图标按钮不需要可见表头，但读屏念到这一格时要有名字
                    <span className="sr-only">{t("usage.colActions")}</span>
                  ) : (
                    t(`usage.${c.key}`)
                  )}
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
                    {/* 无操作列时，这一格是末列，右边缘留白由它补上 */}
                    <td
                      className={`py-2.5 text-right num text-text-secondary ${
                        onClear ? "px-3" : "px-5 md:px-6"
                      }`}
                    >
                      {humanTokens(r.breakdown.audio)}
                    </td>
                    {/* 操作格竖向 padding 收到 py-1：30px 按钮配 py-2.5 会把行从 39.5px
                        顶到 51px（实测）。收成 4px 后这一格 38px，行高仍由模型列的
                        URL 框决定——按钮是白拿的，不加高任何一行。 */}
                    {onClear && (
                      <td className="pl-2 pr-5 md:pr-6 py-1 text-right">
                        <UsageClearMenu
                          target={{
                            model: r.model,
                            baseUrl: r.base_url,
                            label: scopeChipOf(r),
                          }}
                          placement="fixed"
                          onPick={onClear}
                        />
                      </td>
                    )}
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
