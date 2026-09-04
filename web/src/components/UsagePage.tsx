/**
 * 📊 用量 tab 主页面容器。
 *
 * 「Token 用量」一张卡，**一条工具条统管全卡**：周期（今日 / 近7天 / 近30天）+ 时间
 * 粒度（仅今日有意义）+ 刷新 + 清空。此前周期埋在总览段、粒度埋在时间分布段，改一个
 * 要往回滚一整段；筛选控件该一行统管它作用的全部内容。
 *
 * 卡内两栏：左总量 / 模态构成，右时间分布（可切合计 ↔ 按模态）；窄屏自动竖排。
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

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { clearUsageData, getUsageStats } from "@/api";
import { useAsync } from "@/hooks/useAsync";
import type { UsagePeriod, UsageStats } from "@/lib/types";
import { humanTokens } from "@/lib/formatTokens";
import { shortenUrlSet } from "@/lib/modelIdentity";
import { CollapsibleCard } from "./CollapsibleCard";
import { RefreshIntervalInput } from "./RefreshIntervalInput";
import { useRefreshInterval } from "@/hooks/useRefreshInterval";
import { PERIOD_KEYS } from "@/lib/usagePeriods";
import { UsageTodayOverview } from "./UsageTodayOverview";
import { UsageTimelineChart } from "./UsageTimelineChart";
import { UsageBreakdownTable } from "./UsageBreakdownTable";
import { UsageOmniConfig } from "./UsageOmniConfig";
import { UsageClearDialog } from "./UsageClearDialog";
import { UsageClearMenu, type ClearScope } from "./UsageClearMenu";
import { PerfInline } from "./PerfInline";
import { Segmented } from "./Segmented";

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

export function UsagePage() {
  const { t, i18n } = useTranslation();
  const [period, setPeriod] = useState<UsagePeriod>("today");
  const [binMinutes, setBinMinutes] = useState(60);
  // 选了范围才开弹窗；null = 没在清。范围随状态一起带进弹窗，弹窗因此能复述清的是谁。
  const [clearScope, setClearScope] = useState<ClearScope | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  // 刷新周期与性能卡共用一个值（见 useRefreshInterval 的说明）
  const { sec: refreshSec, setSec: setRefreshSec } = useRefreshInterval();

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
  // 按住户设定的周期轮询 + 回到前台时补一次。没有这个的话数字从挂载起就冻住：感知循环一直在烧
  // token，而晚上打开、次日再看还是「今日总览」显示昨天的数（时间桶骨架锚在取数那刻）。
  useEffect(() => {
    const id = setInterval(() => void reload(), refreshSec * 1000);
    const onVisible = () => {
      if (document.visibilityState === "visible") void reload();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [reload, refreshSec]);

  const periodOptions = useMemo(
    () =>
      (Object.keys(PERIOD_KEYS) as UsagePeriod[]).map((k) => ({
        key: k,
        label: t(PERIOD_KEYS[k]),
      })),
    [t],
  );

  const stats = usage.data;
  /**
   * 短地址对照**全卡实际出现的那一组**算一次，浮层与明细表共用。
   *
   * 两处若各算各的：预算不同、输入集合也不同（浮层取时间序列里出现过的地址，表里取明细
   * 行的），而成组压缩会自动放宽到「全组互不相同」为止，于是两边停在不同档上——同一个
   * endpoint 在浮层里可能是 `…om/openai/v1-test`（尾段吃满预算，退化成纯头部省略）、
   * 在表里却是 `api.co…/openai/v1-test`（保住主机头），肉眼不像同一台机器。而这两块就
   * 在同一屏上、读的是同一份数据，拿浮层里某一行去表里找对应那行正是它们并排的理由。
   *
   * 预算取两处里更紧的那个（浮层）：浮层是 max-w 限宽 + CSS truncate，放不下会从**尾部**
   * 截断，而尾段恰是区分 endpoint 的地方——压短保尾就是为了防这个；明细表窄一点无妨，
   * 那里的地址徽记点开就是完整原文。
   */
  const urlLabels = useMemo(
    () => shortenUrlSet((stats?.rows ?? []).map((r) => r.base_url).filter(Boolean), 18),
    [stats],
  );
  const timeLabel = updatedAt
    ? updatedAt.toLocaleTimeString(i18n.language === "en" ? "en-US" : "zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        // 与性能卡同一套：两张卡上下相邻，英文档下一处 12 小时制一处 24 小时制会很扎眼。
        // 用 hourCycle 而不是 hour12: false——后者按 ECMA-402 取的是「该 locale 默认时制的
        // 非 12 小时对偶」，en-US 的默认是 h12，配出来的是 h24（1–24 循环，午夜记作 24），
        // 旧引擎上 00:07 会显示成 24:07。两者不能并列传，hour12 会把 hourCycle 静默丢掉。
        hourCycle: "h23",
      })
    : null;

  return (
    <div className="space-y-6">
      {/* 模型配置卡置顶、可折叠;独立于用量加载(用量请求失败也能在此修配置自救) */}
      <UsageOmniConfig />

      <CollapsibleCard
        title={t("usage.tokenUsageTitle")}
        busy={usage.loading}
        summary={
          stats ? (
            <span className="text-text-secondary">
              {humanTokens(stats.total_tokens)} {t("usage.tokensUnit")}
            </span>
          ) : undefined
        }
        // 「更新于」是关于这张卡数据的元信息、不是控件；放进工具条会把三个控件挤密
        meta={
          timeLabel ? (
            <span className="text-text-tertiary num">
              {t("usage.updatedAt", { time: timeLabel })}
            </span>
          ) : undefined
        }
        toolbar={
        <div className="flex items-center gap-3 flex-wrap">
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
          <RefreshIntervalInput sec={refreshSec} onChange={setRefreshSec} />
          <button
            type="button"
            onClick={() => void reload()}
            className="text-caption px-3 py-1.5 rounded-md border border-border text-text-secondary
                       hover:text-text-primary hover:border-border-strong transition-colors"
          >
            {t("common.refresh")}
          </button>
          {/* 与「刷新」之间多留一道间隔：安全高频动作与不可逆动作不该紧邻 */}
          <div className="ml-2.5">
            <UsageClearMenu onPick={setClearScope} />
          </div>
        </div>
        }
      >

        {/* 错误内联呈现：控件保持挂载，且给重试入口。 */}
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
              {t("common.retry")}
            </button>
          </div>
        )}

        {/* 没有数据时的整格占位。失败已由上面的错误条连带重试入口表达，
            这里不再补一句「暂无用量数据」——那会让人以为「取到了，只是没有数据」。 */}
        {!stats ? (
          usage.error ? null : (
            <div className="py-8 text-center text-text-secondary">
              {usage.loading ? t("usage.loading") : t("usage.noUsageData")}
            </div>
          )
        ) : (
          // 重取时保持画面、只降透明度：不闪骨架、不跳布局
          <div className={usage.loading ? "opacity-60 transition-opacity" : "transition-opacity"}>
            {/* 左栏用 max-content：轨道贴着内容，分隔线两侧的留白就对称了。
                原先是固定宽度而内容撑不满，左侧内容到分隔线的留白明显宽于右侧。
                轨道宽由左栏里较宽的那一块决定——总量那组数字，或环形图＋图例这一行；
                两者都不随调用次数的位数变化，所以分隔线不会随数据跳。
                minmax(0,1fr) 让右栏可压缩到 0，左栏再宽也不会把卡撑破。 */}
              <div className="grid gap-7 lg:grid-cols-[max-content_minmax(0,1fr)]">
              <UsageTodayOverview stats={stats} />
              <div className="lg:border-l lg:border-border lg:pl-7 border-t border-border pt-6 lg:border-t-0 lg:pt-0">
                <UsageTimelineChart
                  stats={stats}
                  binMinutes={binMinutes}
                  urlLabels={urlLabels}
                />
              </div>
            </div>

            <div className="mt-6 pt-6 border-t border-border">
              <UsageBreakdownTable
                stats={stats}
                onClear={setClearScope}
                urlLabels={urlLabels}
              />
            </div>
          </div>
        )}
      </CollapsibleCard>

      {/* 性能监控(精简:工具条 + KPI + 实时率 + Gate;完整版仍在 #perf) */}
      <PerfInline />

      {clearScope && (
        <UsageClearDialog
          scope={clearScope}
          dailyEarliestDate={stats?.daily_earliest_date ?? null}
          dailyLatestDate={stats?.daily_latest_date ?? null}
          onClose={() => setClearScope(null)}
          onCleared={() => {
            setClearScope(null);
            void reload();
          }}
          clear={(s, fromDate) =>
            clearUsageData({
              sinceMs: s.sinceMs,
              // 界面已经把这一天写给用户看了，原样带给后端：日表按盒子时区归日，
              // 提示按浏览器时区算，不带就可能「说的那天」不是「删的那天」。
              fromDate,
              // 只在定点清除时带 model/base_url，且必须成对：前端会抛、后端返 400。
              // 判定看 s.target 有没有，不看 baseUrl 真不真（空串是合法目标）。
              ...(s.target ? { model: s.target.model, baseUrl: s.target.baseUrl } : {}),
            })
          }
        />
      )}
    </div>
  );
}
