/**
 * 📊 用量 tab 主页面容器。
 *
 * 「Token 用量」一张卡，**一条工具条统管全卡**：周期（今日 / 近7天 / 近30天）+ 时间
 * 粒度（仅今日有意义）+ 刷新 + 清空。此前周期埋在总览段、粒度埋在时间分布段，改一个
 * 要往回滚一整段；筛选控件该一行统管它作用的全部内容。
 *
 * 卡内两栏：左总量 / 费用 / 模态构成，右时间分布（可切合计 ↔ 按模态）；窄屏自动竖排。
 * 下面接明细表。
 *
 * 加载态有两处刻意的选择：
 *  - **失败不卸控件**。原先门序是 `loading&&!data → error → data`，error 判在 data 前面，
 *    于是「已有今日数据、切近30天失败」会把整格换成一行红字，而周期选择器就在被卸掉的
 *    组件里——没有任何控件可退回，只能切 tab 再回来（周期还会重置）。现在改为优先保留
 *    已有数据、错误以内联条呈现并带重试。
 *  - **重取保持画面**。旧数据留在原位并降透明度，不闪骨架、不跳布局；同时挂 aria-busy，
 *    否则读屏用户会把上一周期的数字当成新周期的。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { clearUsageData, getUsageStats } from "@/api";
import { useAsync } from "@/hooks/useAsync";
import type { UsagePeriod, UsageStats, UsageTimelinePoint } from "@/lib/types";
import {
  costInputOf,
  estimateCost,
  loadPricing,
  pricingFor,
  savePricing,
  type UsagePricing,
} from "@/lib/usagePricing";
import { UsageTodayOverview } from "./UsageTodayOverview";
import { UsageTimelineChart } from "./UsageTimelineChart";
import { UsageBreakdownTable } from "./UsageBreakdownTable";
import { UsageOmniConfig } from "./UsageOmniConfig";
import { UsagePricingDialog } from "./UsagePricingDialog";
import { UsageClearDialog } from "./UsageClearDialog";
import { PerfInline } from "./PerfInline";
import { Segmented } from "./Segmented";

const PERIOD_KEYS: Record<UsagePeriod, string> = {
  today: "usage.periodToday",
  week: "usage.periodWeek",
  month: "usage.periodMonth",
};

/**
 * 时间粒度档位。15 分而非 10 分：24 小时切 10 分是 144 桶，1440 视口下每桶只剩
 * 5.1px、1024 下 2.8px，柱子糊成一片；15 分是 96 桶（7.6px / 4.2px），且「一刻钟」
 * 更贴人对时间的分法。后端 bin 收任意分钟数，换档不需要动服务端。
 */
const BIN_OPTIONS = [
  { minutes: 15, labelKey: "usage.bin15min" },
  { minutes: 60, labelKey: "usage.bin1hour" },
  { minutes: 180, labelKey: "usage.bin3hour" },
];

/** 自动刷新周期：与下方性能卡一致，避免同一页两套刷新行为。 */
const REFRESH_MS = 30_000;

export function UsagePage() {
  const { t, i18n } = useTranslation();
  const [period, setPeriod] = useState<UsagePeriod>("today");
  const [binMinutes, setBinMinutes] = useState(60);
  const [pricing, setPricing] = useState<UsagePricing>(() => loadPricing());
  const [pricingOpen, setPricingOpen] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  const usage = useAsync<UsageStats>(
    () => getUsageStats(period, period === "today" ? binMinutes : undefined),
    [period, binMinutes],
    { errorLabel: t("usage.loadError") },
  );

  // 数据落地就记一次时刻，让「更新于 HH:MM」说的是数据的新鲜度而不是渲染时刻
  useEffect(() => {
    if (usage.data) setUpdatedAt(new Date());
  }, [usage.data]);

  const reload = usage.reload;
  // 30s 轮询 + 回到前台时补一次。没有这个的话数字从挂载起就冻住：感知循环一直在烧
  // token，而晚上打开、次日再看还是「今日总览」显示昨天的数（时间桶骨架锚在取数那刻）。
  useEffect(() => {
    const id = setInterval(() => void reload(), REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void reload();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [reload]);

  const savePricingAndClose = useCallback((next: UsagePricing) => {
    setPricing(next);
    savePricing(next);
    setPricingOpen(false);
  }, []);

  // 单桶费用：图表本身不认识单价，由这里注入一个格式化好的字符串
  const costLabelOf = useCallback(
    (p: UsageTimelinePoint): string | null => {
      if (p.tokens <= 0) return null;
      // 分桶数据没有按模型拆分，只能用「本周期出现过的模型」里的第一档单价近似；
      // 单模型（家用常态）下即精确，多模型时是近似，故仍带 ≈ 前缀。
      const model = usage.data?.rows[0]?.model ?? "";
      const pr = pricingFor(pricing, model);
      const v = estimateCost(
        costInputOf({
          input: p.text + p.video + p.audio,
          output: p.output,
          cache: p.cache,
          video: p.video,
          audio: p.audio,
        }),
        pr,
        pricing.per,
      ).total;
      const s = v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
      return `≈ ${pricing.currency}${s}`;
    },
    [pricing, usage.data],
  );

  const periodOptions = useMemo(
    () =>
      (Object.keys(PERIOD_KEYS) as UsagePeriod[]).map((k) => ({
        key: k,
        label: t(PERIOD_KEYS[k]),
      })),
    [t],
  );

  const stats = usage.data;
  const timeLabel = updatedAt
    ? updatedAt.toLocaleTimeString(i18n.language === "en" ? "en-US" : "zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

  return (
    <div className="space-y-6">
      {/* 模型配置卡置顶、可折叠;独立于用量加载(用量请求失败也能在此修配置自救) */}
      <UsageOmniConfig />

      <section
        className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
        aria-busy={usage.loading}
      >
        <h2 className="text-section-title mb-4">{t("usage.tokenUsageTitle")}</h2>

        {/* 一条工具条统管全卡 */}
        <div className="flex items-center gap-3 flex-wrap mb-5">
          <div className="flex items-center gap-2">
            <span className="text-caption text-text-tertiary">{t("usage.statsPeriodAria")}</span>
            <Segmented
              ariaLabel={t("usage.statsPeriodAria")}
              value={period}
              onChange={setPeriod}
              options={periodOptions}
            />
          </div>
          {period === "today" && (
            <div className="flex items-center gap-2">
              <span className="text-caption text-text-tertiary">
                {t("usage.granularityAria")}
              </span>
              <Segmented
                ariaLabel={t("usage.granularityAria")}
                value={binMinutes}
                onChange={setBinMinutes}
                options={BIN_OPTIONS.map((b) => ({
                  key: b.minutes,
                  label: t(b.labelKey),
                }))}
              />
            </div>
          )}
          <div className="flex-1 min-w-2" />
          {timeLabel && (
            <span className="text-caption text-text-tertiary num">
              {t("usage.updatedAt", { time: timeLabel })}
            </span>
          )}
          <button
            type="button"
            onClick={() => void reload()}
            className="text-caption px-3 py-1.5 rounded-md border border-border text-text-secondary
                       hover:text-text-primary hover:border-border-strong transition-colors"
          >
            {t("usage.refresh")}
          </button>
          <button
            type="button"
            onClick={() => setClearOpen(true)}
            className="text-caption text-text-secondary hover:text-error transition-colors"
          >
            {t("usage.clearData")}
          </button>
        </div>

        {/* 错误内联呈现：控件保持挂载，且给重试入口。首次加载还没有任何数据时才占整格。 */}
        {usage.error && (
          <div
            className="mb-5 flex items-center justify-between gap-3 flex-wrap rounded-lg
                       bg-error-bg text-error text-caption px-3.5 py-2.5"
            role="alert"
          >
            <span>{usage.error.message}</span>
            <button
              type="button"
              onClick={() => void reload()}
              className="px-2.5 py-1 rounded-md border border-error hover:bg-error-bg
                         transition-colors"
            >
              {t("usage.retry")}
            </button>
          </div>
        )}

        {!stats ? (
          <div className="py-8 text-center text-text-secondary">
            {usage.loading ? t("usage.loading") : t("usage.noUsageData")}
          </div>
        ) : (
          // 重取时保持画面、只降透明度：不闪骨架、不跳布局
          <div className={usage.loading ? "opacity-60 transition-opacity" : "transition-opacity"}>
            <div className="grid gap-7 lg:grid-cols-[minmax(300px,340px)_minmax(0,1fr)]">
              <UsageTodayOverview
                stats={stats}
                pricing={pricing}
                onOpenPricing={() => setPricingOpen(true)}
              />
              <div className="lg:border-l lg:border-border lg:pl-7 border-t border-border pt-6 lg:border-t-0 lg:pt-0">
                <UsageTimelineChart
                  stats={stats}
                  binMinutes={binMinutes}
                  costLabelOf={costLabelOf}
                />
              </div>
            </div>

            <div className="mt-6 pt-6 border-t border-border">
              <UsageBreakdownTable stats={stats} pricing={pricing} />
            </div>
          </div>
        )}
      </section>

      {/* 性能监控(精简:工具条 + KPI + 实时率 + Gate;完整版仍在 #perf) */}
      <PerfInline />

      {pricingOpen && stats && (
        <UsagePricingDialog
          stats={stats}
          pricing={pricing}
          onSave={savePricingAndClose}
          onClose={() => setPricingOpen(false)}
        />
      )}
      {clearOpen && (
        <UsageClearDialog
          onClose={() => setClearOpen(false)}
          onCleared={() => {
            setClearOpen(false);
            void reload();
          }}
          clear={clearUsageData}
        />
      )}
    </div>
  );
}
