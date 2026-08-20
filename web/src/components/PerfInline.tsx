/**
 * 「模型」tab 内联的精简性能视图:只含 工具条 + KPI 卡 + 实时率 + Gate 过滤率。
 *
 * 完整性能页仍在 #perf(PerfPage)保留全部区块;这里只复用前几个块,
 * 且只拉这几块需要的接口(summary / rtf / gate),不发起其余 perf 请求。
 *
 * 「紧凑显示」把实时率与 Gate 过滤率并排放一行——两张图都是同一时间窗、同一分桶的
 * 折线,并排比上下叠更容易对着看「这一段过滤率高的时候实时率是不是也掉了」。仅在够宽
 * (xl)时真并排,窄屏仍然竖排,否则两张折线各自只剩一半宽、点全糊在一起。
 * 开关状态与主题/语言同一套路持久化到 localStorage。
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getPerfGatePassRate,
  getPerfRtfSeries,
  getPerfSummary,
} from "@/api";
import { useAsync } from "@/hooks/useAsync";
import { WINDOW_MS, perfWindows, defaultBucket } from "@/lib/perfBucket";
import type { PerfWindow } from "@/lib/types";
import { PerfKpiCards } from "./PerfKpiCards";
import { PerfRtfChart } from "./PerfRtfChart";
import { PerfGateChart } from "./PerfGateChart";

const COMPACT_KEY = "web:perf:compact";

function readCompact(): boolean {
  if (typeof localStorage === "undefined") return false;
  return localStorage.getItem(COMPACT_KEY) === "1";
}

export function PerfInline() {
  const { t, i18n } = useTranslation();
  const [compact, setCompact] = useState(readCompact);
  // 窗口选项随语言重算;memo 在 i18n.language 不变时保持引用稳定。
  const windows = useMemo(() => perfWindows(), [i18n.language]);
  const [windowKey, setWindow] = useState<PerfWindow>("1h");
  const bucket = defaultBucket(windowKey);
  const windowMs = WINDOW_MS[windowKey];

  const summary = useAsync(() => getPerfSummary(windowKey), [windowKey], {
    errorLabel: t("perf.errSummary"),
  });
  const rtf = useAsync(() => getPerfRtfSeries(windowKey, bucket), [windowKey, bucket], {
    errorLabel: t("perf.errRtfSeries"),
  });
  const gate = useAsync(() => getPerfGatePassRate(windowKey, bucket), [windowKey, bucket], {
    errorLabel: t("perf.errGate"),
  });

  const reloadAll = () => {
    summary.reload();
    rtf.reload();
    gate.reload();
  };

  // 30s 自动刷新,窗口切换重置 timer。
  useEffect(() => {
    const id = setInterval(reloadAll, 30_000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowKey]);

  return (
    <section className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6">
      {/* 标题独占一行(与 Token 用量 / 模型配置 一致,更显著) */}
      <h2 className="text-section-title mb-4">{t("perf.inlineTitle")}</h2>

      {/* 窗口切换 + 刷新(标题下方单独一行) */}
      <div className="flex items-center justify-between flex-wrap gap-3 mb-4">
        <div className="flex gap-1 bg-bg-primary rounded-lg p-1" role="tablist">
          {windows.map((w) => {
            const on = windowKey === w.key;
            return (
              <button
                key={w.key}
                type="button"
                role="tab"
                aria-selected={on}
                onClick={() => setWindow(w.key)}
                className={`text-caption px-3 py-1 rounded-lg transition-colors ${
                  on
                    ? "bg-bg-secondary text-text-primary shadow-sm"
                    : "text-text-secondary hover:text-text-primary"
                }`}
              >
                {w.label}
              </button>
            );
          })}
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-caption text-text-secondary cursor-pointer">
            <input
              type="checkbox"
              checked={compact}
              aria-label={t("perf.compactAria")}
              onChange={(e) => {
                setCompact(e.target.checked);
                try {
                  localStorage.setItem(COMPACT_KEY, e.target.checked ? "1" : "0");
                } catch {
                  // 存储不可用（隐私模式）→ 本次会话内仍生效，不报错
                }
              }}
              className="accent-brand-primary"
            />
            {t("perf.compact")}
          </label>
          <span className="text-caption text-text-tertiary">{t("perf.autoRefresh")}</span>
          <button
            type="button"
            onClick={reloadAll}
            className="text-caption px-3 py-1.5 rounded-md border border-border text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
          >
            {t("perf.manualRefresh")}
          </button>
        </div>
      </div>

      {/* KPI 卡 */}
      <PerfKpiCards state={summary} embedded />

      {/* 实时率 + Gate 过滤率：紧凑时并排（仅 xl 起真并排，窄屏仍竖排） */}
      <div
        className={`mt-6 pt-6 border-t border-border ${
          compact ? "grid gap-7 xl:grid-cols-2" : ""
        }`}
      >
        <PerfRtfChart state={rtf} bucket={bucket} windowMs={windowMs} embedded />
        <div
          className={
            compact
              ? "xl:border-l xl:border-border xl:pl-7 border-t border-border pt-6 xl:border-t-0 xl:pt-0"
              : "mt-6 pt-6 border-t border-border"
          }
        >
          <PerfGateChart state={gate} bucket={bucket} windowMs={windowMs} embedded />
        </div>
      </div>
    </section>
  );
}
