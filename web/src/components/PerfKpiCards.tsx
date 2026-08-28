/**
 * 7 张 KPI 卡:轮次(含应处理) / Gate 过滤率 / 窗口丢弃率 / Omni 错误率 / 耗时 P95 /
 * omni 耗时 P95 / Agent 调用。
 *
 * 阈值颜色:>5% drop / >5% omni error / 耗时跟不上 → 红字提醒,其余中性。
 * 耗时那两格显示毫秒,但「跟不上」的判据由后端给(p95_behind_*):逐行的 耗时/窗口
 * 排完序第 95 个 > 1。显示与判据分开,是因为 P95 是尾部统计量、不能拿它去跟某个
 * 跨度均值比——那样两个方向都会误判。
 *
 * Omni 错误时间分布另起一行,见 PerfOmniErrorChart。
 */

import { useTranslation } from "react-i18next";
import type { AsyncState } from "@/hooks/useAsync";
import type { PerfSummary } from "@/lib/types";

interface Props {
  state: AsyncState<PerfSummary>;
  /** 嵌入「性能监测」大卡时:KPI 小卡改用 bg-primary 以在二级底色上显出来,空/错态去壳。 */
  embedded?: boolean;
}

interface KpiCardProps {
  label: string;
  hint?: string;
  value: string;
  /** 警告态:数值越界,文字标红。 */
  warn?: boolean;
  /** 主数字下方追加的次级数字行(如 skip 卡里的 video/audio 拆分)。 */
  sub?: { label: string; value: string; warn?: boolean }[];
}

function KpiCard({ label, hint, value, warn, sub, embedded }: KpiCardProps & { embedded?: boolean }) {
  return (
    <div
      className={`rounded-xl border border-border shadow-sm p-4 md:p-5 ${
        embedded ? "bg-bg-primary" : "bg-bg-secondary"
      }`}
    >
      <div className="text-caption text-text-tertiary mb-1.5">{label}</div>
      <div
        className={`num text-display ${warn ? "text-error" : "text-text-primary"}`}
      >
        {value}
      </div>
      {sub && sub.length > 0 && (
        <div className="text-caption flex flex-wrap gap-x-3 mt-1.5">
          {sub.map((s) => (
            <span key={s.label} className="text-text-tertiary">
              {s.label}{" "}
              <span
                className={`num ${
                  s.warn ? "text-error font-semibold" : "text-text-secondary"
                }`}
              >
                {s.value}
              </span>
            </span>
          ))}
        </div>
      )}
      {hint && !sub && (
        <div className="text-caption text-text-secondary mt-1">{hint}</div>
      )}
    </div>
  );
}

function pct(v: number): string {
  return `${(v * 100).toFixed(1)}%`;
}

export function PerfKpiCards({ state, embedded = false }: Props) {
  const { t } = useTranslation();
  if (state.loading && !state.data) {
    return (
      <div className={embedded ? "p-8 text-center text-text-secondary" : "rounded-xl bg-bg-secondary border border-border shadow-sm p-8 text-center text-text-secondary"}>
        {t("perf.loading")}
      </div>
    );
  }
  if (state.error) {
    return (
      <div className={embedded ? "p-8 text-center text-error" : "rounded-xl bg-bg-secondary border border-error shadow-sm p-8 text-center text-error"}>
        {state.error.message}
      </div>
    );
  }
  if (!state.data) return null;

  const s = state.data;
  // 空窗口提示
  if (s.cycle_count === 0) {
    return (
      <div className={embedded ? "p-8 text-center text-text-secondary" : "rounded-xl bg-bg-secondary border border-border shadow-sm p-8 text-center text-text-secondary"}>
        {t("perf.kpiEmptyTrace")}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-7 gap-3">
      <KpiCard
        embedded={embedded}
        label={t("perf.kpiCycle")}
        value={s.cycle_count.toLocaleString()}
        sub={[
          {
            label: t("perf.kpiShouldProcess"),
            value: (s.cycle_count + s.dropped_count).toLocaleString(),
          },
        ]}
      />
      <KpiCard
        embedded={embedded}
        label={t("perf.kpiGateFilterRate")}
        value={pct(s.skip_rate)}
        hint={t("perf.kpiGateFilterRateHint")}
      />
      <KpiCard
        embedded={embedded}
        label={t("perf.kpiDropRate")}
        value={pct(s.drop_rate)}
        hint={t("perf.kpiDropRateHint")}
        warn={s.drop_rate > 0.05}
      />
      <KpiCard
        embedded={embedded}
        label={t("perf.kpiOmniErrorRate")}
        value={pct(s.omni_error_rate)}
        hint={t("perf.kpiOmniErrorRateHint")}
        warn={s.omni_error_rate > 0.05}
      />
      <KpiCard
        embedded={embedded}
        label={t("perf.kpiLatencyP95")}
        value={`${Math.round(s.p95_ms_e2e)} ms`}
        hint={t("perf.kpiLatencyP95Hint")}
        warn={s.p95_behind_e2e}
      />
      <KpiCard
        embedded={embedded}
        label={t("perf.kpiOmniLatencyP95")}
        value={`${Math.round(s.p95_ms_omni)} ms`}
        hint={t("perf.kpiOmniLatencyP95Hint")}
        warn={s.p95_behind_omni}
      />
      <KpiCard
        embedded={embedded}
        label={t("perf.kpiAgentCall")}
        value={s.agent_call_count.toLocaleString()}
        hint={t("perf.kpiAgentCallHint")}
      />
    </div>
  );
}
