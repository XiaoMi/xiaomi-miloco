/**
 * 📊 用量 tab 主页面容器。
 *
 * 顶部统一的「周期」选择器（今日 / 近7天 / 近30天）控制整页作用域：
 * 总览、时间分布、明细三块都按所选周期展示。数据只取一次
 * （getUsageStats 同时给出该周期的 timeline + 汇总 + model×type 明细），三块共用。
 *
 * 「时间粒度」(bin) 只对今日的时间分布有意义，放在时间分布卡内、仅今日显示。
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { getUsageStats } from "@/api";
import { useAsync } from "@/hooks/useAsync";
import type { UsagePeriod, UsageStats } from "@/lib/types";
import { UsageTodayOverview } from "./UsageTodayOverview";
import { UsageTimelineChart } from "./UsageTimelineChart";
import { UsageBreakdownTable } from "./UsageBreakdownTable";
import { UsageOmniConfig } from "./UsageOmniConfig";
import { SplitModelConfig } from "./SplitModelConfig";
import { PerfInline } from "./PerfInline";

export function UsagePage() {
  const { t } = useTranslation();
  const [period, setPeriod] = useState<UsagePeriod>("today");
  const [binMinutes, setBinMinutes] = useState(60);
  // 清空用量后自增以触发重取
  const [refreshKey, setRefreshKey] = useState(0);
  const usage = useAsync<UsageStats>(
    () => getUsageStats(period, period === "today" ? binMinutes : undefined),
    [period, binMinutes, refreshKey],
    { errorLabel: t("usage.loadError") },
  );

  // 全页两大格:① 模型配置 ② Token 用量(总览/时间分布/明细 合为一格、内部用分隔线分段)
  return (
    <div className="space-y-6">
      {/* 模型配置卡置顶、可折叠;独立于用量加载(用量请求失败也能在此修配置自救) */}
      <UsageOmniConfig />

      {/* 截图模式双模型配置（可选，配了则截图模式下自动拆分） */}
      <SplitModelConfig />

      <section className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6">
        <h2 className="text-section-title mb-4">{t("usage.tokenUsageTitle")}</h2>
        {usage.loading && !usage.data ? (
          <div className="py-8 text-center text-text-secondary">{t("usage.loading")}</div>
        ) : usage.error ? (
          <div className="py-8 text-center text-error">{usage.error.message}</div>
        ) : usage.data ? (
          <>
            <UsageTodayOverview
              stats={usage.data}
              period={period}
              onPeriodChange={setPeriod}
              onCleared={() => setRefreshKey((k) => k + 1)}
              embedded
            />
            <div className="mt-6 pt-6 border-t border-border">
              <UsageTimelineChart
                stats={usage.data}
                binMinutes={binMinutes}
                onBinChange={setBinMinutes}
                embedded
              />
            </div>
            <div className="mt-6 pt-6 border-t border-border">
              <UsageBreakdownTable stats={usage.data} embedded />
            </div>
          </>
        ) : null}
      </section>

      {/* 性能监控(精简:工具条 + KPI + 实时率 + Gate;完整版仍在 #perf) */}
      <PerfInline />
    </div>
  );
}
