/**
 * 最近 Agent 调用列表。
 *
 * 数据源 /api/agent_runs:每次 agent turn 一行。
 * 表头:时间 / 来源 / 指令 / 耗时 / LLM 次数 / Tool 次数 / 慢 tool / 成功状态
 *
 * 同 trace_id 下 N 个 agent_run 在表里独立显示,不再被聚合覆盖。
 */

import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import type { AsyncState } from "@/hooks/useAsync";
import { formatPerfTs } from "@/lib/perfBucket";
import type { PerfAgentRun } from "@/lib/types";

interface Props {
  state: AsyncState<PerfAgentRun[]>;
  windowMs: number;
}

const QUERY_PREVIEW_MAX = 80;

function previewQuery(q: string | null): string {
  if (!q) return "—";
  const oneLine = q.replace(/\s+/g, " ").trim();
  if (oneLine.length <= QUERY_PREVIEW_MAX) return oneLine;
  return `${oneLine.slice(0, QUERY_PREVIEW_MAX)}…`;
}

const SOURCE_KEY: Record<string, string> = {
  rule: "perf.sourceRule",
  interaction: "perf.sourceInteraction",
  suggestion: "perf.sourceSuggestion",
};

function sourceLabel(s: string, t: TFunction): string {
  const key = SOURCE_KEY[s];
  return key ? t(key) : s;
}

export function PerfAgentList({ state, windowMs }: Props) {
  const { t } = useTranslation();
  const rows = state.data ?? [];

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-labelledby="perf-agent-title"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-2 mb-4">
        <h2 id="perf-agent-title" className="text-title">
          {t("perf.agentTitle")}
        </h2>
        <span className="text-caption text-text-secondary">
          {t("perf.agentSubtitle")}
        </span>
      </div>

      {state.loading && !state.data ? (
        <div className="py-8 text-center text-text-secondary">{t("perf.loading")}</div>
      ) : state.error ? (
        <div className="py-8 text-center text-error">{state.error.message}</div>
      ) : rows.length > 0 ? (
        <Table rows={rows} windowMs={windowMs} t={t} />
      ) : (
        <div className="py-8 text-center text-text-secondary">
          {t("perf.agentEmpty")}
        </div>
      )}
    </section>
  );
}

function Table({ rows, windowMs, t }: { rows: PerfAgentRun[]; windowMs: number; t: TFunction }) {
  return (
    <div className="text-caption overflow-x-auto -mx-5 md:-mx-6">
      <table className="w-full">
        <thead>
          <tr className="text-text-secondary border-b border-border">
            <th className="text-left px-5 md:px-6 py-2">{t("perf.colTime")}</th>
            <th className="text-left px-3 py-2">{t("perf.colSource")}</th>
            <th className="text-left px-3 py-2">{t("perf.colQuery")}</th>
            <th className="text-right px-3 py-2 num">{t("perf.colDurationMs")}</th>
            <th className="text-right px-3 py-2 num">{t("perf.colLlm")}</th>
            <th className="text-right px-3 py-2 num">{t("perf.colTool")}</th>
            <th className="text-left px-3 py-2">{t("perf.colSlowTool")}</th>
            <th className="text-center px-5 md:px-6 py-2">{t("perf.colStatus")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const failed = r.success === 0 || (r.error_count ?? 0) > 0;
            return (
              <tr
                key={r.run_id}
                className="border-b border-border last:border-b-0 hover:bg-bg-primary"
              >
                <td className="px-5 md:px-6 py-2 mono text-text-secondary whitespace-nowrap">
                  {formatPerfTs(r.timestamp, { spanMs: windowMs, withSec: true })}
                </td>
                <td className="px-3 py-2 text-text-secondary whitespace-nowrap">
                  {sourceLabel(r.source, t)}
                </td>
                <td className="px-3 py-2 text-text-primary max-w-md">
                  <span className="break-all" title={r.query ?? ""}>
                    {previewQuery(r.query)}
                  </span>
                </td>
                <td className="px-3 py-2 num text-right text-text-primary">
                  {r.duration_ms != null ? r.duration_ms.toFixed(0) : "—"}
                </td>
                <td className="px-3 py-2 num text-right text-text-secondary">
                  {r.llm_call_count ?? "—"}
                </td>
                <td className="px-3 py-2 num text-right text-text-secondary">
                  {r.tool_call_count ?? "—"}
                </td>
                <td className="px-3 py-2 mono text-text-secondary">
                  {r.slowest_tool_name ?? "—"}
                </td>
                <td className="px-5 md:px-6 py-2 text-center">
                  {failed ? (
                    <span
                      className="text-error font-semibold"
                      title={r.error_msg ?? t("perf.statusFailedTip")}
                    >
                      {t("perf.statusFailed")}
                    </span>
                  ) : r.success === 1 ? (
                    <span className="text-success">{t("perf.statusSuccess")}</span>
                  ) : (
                    <span className="text-text-tertiary">{t("perf.statusRunning")}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
