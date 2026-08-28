/**
 * 原始 trace 列表。
 *
 * 表头:时间 / 设备 / 数据包长 / 处理耗时 / 输入类型 / Agent / 丢弃。
 * 行点击可展开子行,显示 8 阶段耗时分解。
 *
 * 没有单独的「耗时/窗口」比值列:数据包长与处理耗时两列都在表上,比值是这两个数一除,
 * 多一列等于把同一件事再说一遍。「跟不上」改染在处理耗时那一格,**判据沿用原来那一列
 * 的**(端到端比值超过 1,回退到 cycle 比值),没有换标准;它含入队等待、而等待没有单独
 * 成列,所以会出现「表上两列相除没超、格子却红了」——那一格挂了 title 说明这件事。
 *
 * 没接 drawer / trace 详情接口 —— 留给二期。
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import type { AsyncState } from "@/hooks/useAsync";
import { formatPerfTs } from "@/lib/perfBucket";
import type { PerfTraceRow } from "@/lib/types";

interface Props {
  state: AsyncState<PerfTraceRow[]>;
  windowMs: number;
}

const STAGE_KEYS = [
  "decode_ms",
  "collect_ms",
  "convert_ms",
  "gate_ms",
  "identity_ms",
  "omni_ms",
  "log_ms",
] as const;

const STAGE_LABEL: Record<(typeof STAGE_KEYS)[number], string> = {
  decode_ms: "decode",
  collect_ms: "collect",
  convert_ms: "convert",
  gate_ms: "gate",
  identity_ms: "identity",
  omni_ms: "omni",
  log_ms: "log",
};

export function PerfTraceList({ state, windowMs }: Props) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-labelledby="perf-trace-title"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-2 mb-4">
        <h2 id="perf-trace-title" className="text-title">
          {t("perf.traceTitle")}
        </h2>
        <span className="text-caption text-text-secondary">
          {t("perf.traceSubtitle")}
        </span>
      </div>

      {state.loading && !state.data ? (
        <div className="py-8 text-center text-text-secondary">{t("perf.loading")}</div>
      ) : state.error ? (
        <div className="py-8 text-center text-error">{state.error.message}</div>
      ) : state.data && state.data.length > 0 ? (
        <Table
          rows={state.data}
          expanded={expanded}
          windowMs={windowMs}
          onToggle={(id) => setExpanded((cur) => (cur === id ? null : id))}
          t={t}
        />
      ) : (
        <div className="py-8 text-center text-text-secondary">
          {t("perf.traceEmpty")}
        </div>
      )}
    </section>
  );
}

interface TableProps {
  rows: PerfTraceRow[];
  expanded: string | null;
  windowMs: number;
  onToggle: (id: string) => void;
  t: TFunction;
}

function Table({ rows, expanded, windowMs, onToggle, t }: TableProps) {
  return (
    <div className="text-caption overflow-x-auto -mx-5 md:-mx-6">
      <table className="w-full">
        <thead>
          <tr className="text-text-secondary border-b border-border">
            <th className="text-left px-5 md:px-6 py-2">{t("perf.colTime")}</th>
            <th className="text-right px-3 py-2 num">{t("perf.traceColDevice")}</th>
            <th className="text-right px-3 py-2 num">{t("perf.traceColWindowMs")}</th>
            <th className="text-right px-3 py-2 num">{t("perf.traceColCycleMs")}</th>
            <th className="text-center px-3 py-2">{t("perf.traceColInputType")}</th>
            <th className="text-center px-3 py-2">{t("perf.traceColAgent")}</th>
            <th className="text-right px-5 md:px-6 py-2 num">{t("perf.traceColDropped")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const isOpen = expanded === r.trace_id;
            const hasAgent = r.has_agent_turn === 1;
            const hasDropped = r.dropped_windows_total > 0;
            return (
              <Row
                key={r.trace_id}
                row={r}
                hasAgent={hasAgent}
                hasDropped={hasDropped}
                isOpen={isOpen}
                windowMs={windowMs}
                onToggle={() => onToggle(r.trace_id)}
                t={t}
              />
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

interface RowProps {
  row: PerfTraceRow;
  hasAgent: boolean;
  hasDropped: boolean;
  isOpen: boolean;
  windowMs: number;
  onToggle: () => void;
  t: TFunction;
}

function ChannelPass({
  videoPass,
  audioPass,
  holdPass,
  omniErrorCount,
  t,
}: {
  videoPass: boolean;
  audioPass: boolean;
  holdPass: boolean;
  omniErrorCount: number;
  t: TFunction;
}) {
  // 表示本轮提交给 omni 的数据类型:有 video 帧就走视觉链路(video),
  // 仅 audio 才走纯音频链路(audio),都没过 → skip。
  // hold 滞回:visual 未变但距上次通过 ≤ hold_duration_sec,仍走 video 路由——
  // "video (hold)" = hold + audio 还在;"video (full hold)" = visual + audio
  // 都没过,纯靠滞回拉起,后者通常对应真空/睡/短暂离开,诊断价值不同。
  // omni 调用失败时 gate_pass 字段在 placeholder result 中为 0,跟"gate 真没过"
  // 字面相同——优先用 omni_error_count 区分,避免误读为 gate skip。
  if (omniErrorCount > 0) {
    return <span className="text-caption-mono text-error">{t("perf.channelOmniFailed")}</span>;
  }
  if (videoPass) {
    return <span className="text-caption-mono text-text-primary">video</span>;
  }
  if (holdPass) {
    // hold:visual 没变但 audio 还在 → 人在但静止 / 电视声等
    // full hold:visual + audio 都没过,纯靠滞回拉起 → 真空 / 睡 / 短暂离开
    const detail = audioPass ? t("perf.channelHold") : t("perf.channelFullHold");
    const tip = audioPass
      ? t("perf.channelHoldTip")
      : t("perf.channelFullHoldTip");
    return (
      <span className="text-caption-mono text-text-primary" title={tip}>
        video<span className="text-text-tertiary"> ({detail})</span>
      </span>
    );
  }
  if (audioPass) {
    return <span className="text-caption-mono text-text-primary">audio</span>;
  }
  return <span className="text-text-tertiary">—</span>;
}

function Row({
  row,
  hasAgent,
  hasDropped,
  isOpen,
  windowMs,
  onToggle,
  t,
}: RowProps) {
  const r = row;
  // 「这一轮没跟上」：沿用原先那一列的判据（端到端比值 > 1，回退 cycle 比值），只是
  // 把它从独立一格挪到耗时那一格上染色，判据本身一字未改。
  const ratio = r.rtf_e2e ?? r.rtf;
  const overWindow = ratio != null && ratio > 1;
  return (
    <>
      <tr
        className={`border-b border-border last:border-b-0 cursor-pointer transition-colors ${
          isOpen ? "bg-bg-tertiary" : "hover:bg-bg-primary"
        }`}
        onClick={onToggle}
      >
        <td className="px-5 md:px-6 py-2.5 num text-text-primary">
          {r.cycle_error_msg != null && (
            <span
              className="text-error font-bold mr-1.5"
              title={r.cycle_error_msg}
            >
              ⚠
            </span>
          )}
          {formatPerfTs(r.timestamp, { spanMs: windowMs, withSec: true })}
        </td>
        <td className="px-3 py-2.5 text-right num text-text-secondary">
          {r.device_count ?? "—"}
        </td>
        <td className="px-3 py-2.5 text-right num text-text-tertiary">
          {r.window_duration_ms == null
            ? "—"
            : r.window_duration_ms.toFixed(0)}
        </td>
        <td
          className={`px-3 py-2.5 text-right num ${
            overWindow ? "text-error font-semibold" : "text-text-primary"
          }`}
          // 判据含入队等待，而等待没有单独成列，故标红可能与「两列相除」的直觉不符
          title={overWindow ? t("perf.traceCycleBehind") : undefined}
        >
          {r.cycle_total_ms == null ? "—" : r.cycle_total_ms.toFixed(0)}
        </td>
        <td className="px-3 py-2.5 text-center">
          <ChannelPass
            videoPass={r.gate_video_pass === 1}
            audioPass={r.gate_audio_pass === 1}
            holdPass={r.gate_hold_pass === 1}
            omniErrorCount={r.omni_error_count ?? 0}
            t={t}
          />
        </td>
        <td className="px-3 py-2.5 text-center">
          {hasAgent ? (
            <span className="status-dot status-dot-brand" />
          ) : (
            <span className="text-text-tertiary">—</span>
          )}
        </td>
        <td
          className={`px-5 md:px-6 py-2.5 text-right num ${
            hasDropped ? "text-error font-semibold" : "text-text-tertiary"
          }`}
        >
          {r.dropped_windows_total > 0 ? r.dropped_windows_total : "—"}
        </td>
      </tr>
      {isOpen && (
        <tr className="bg-bg-tertiary border-b border-border">
          <td colSpan={7} className="px-5 md:px-6 py-3">
            <ExpandedDetail row={r} t={t} />
          </td>
        </tr>
      )}
    </>
  );
}

function ExpandedDetail({ row, t }: { row: PerfTraceRow; t: TFunction }) {
  const cycle = row.cycle_total_ms ?? 0;
  const windowMs = row.window_duration_ms ?? 0;
  const stages = STAGE_KEYS.map((k) => {
    const ms = row[k];
    return {
      key: k,
      label: STAGE_LABEL[k],
      ms: ms ?? 0,
      pct: cycle > 0 && ms != null ? (ms / cycle) * 100 : 0,
    };
  });
  // 进度条按"包长度"作为基线 — 一眼看清是否赶得上实时。
  // 若 windowMs 缺失则回退到阶段内最大值,至少能看相对比例。
  const baseline = windowMs > 0 ? windowMs : Math.max(...stages.map((s) => s.ms), 1);
  const cycleOverWindow =
    windowMs > 0
      ? t("perf.traceCycleOverWindow", { pct: (cycle / windowMs * 100).toFixed(0) })
      : "";

  return (
    <div className="space-y-2">
      {row.cycle_error_msg != null && (
        <div className="text-caption text-error font-semibold flex items-start gap-1.5">
          <span className="shrink-0">{t("perf.traceCycleError")}</span>
          <span className="break-all">{row.cycle_error_msg}</span>
          <span className="text-text-tertiary font-normal shrink-0">
            {t("perf.traceTracebackHint")}
          </span>
        </div>
      )}
      <div className="text-caption text-text-tertiary flex flex-wrap items-center gap-x-4 gap-y-1">
        <span className="mono">trace_id: {row.trace_id}</span>
        {windowMs > 0 && (
          <span>
            {t("perf.tracePacketLen")}{" "}
            <span className="num text-text-secondary">
              {windowMs.toFixed(0)} ms
            </span>
          </span>
        )}
        {cycleOverWindow && (
          <span
            className={
              cycle > windowMs ? "text-error font-semibold" : "text-text-secondary"
            }
          >
            {cycleOverWindow}
          </span>
        )}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1.5">
        {stages.map((s) => {
          const overWindow = windowMs > 0 && s.ms > windowMs;
          return (
            <div key={s.key} className="flex items-center gap-2 text-caption">
              <span
                className="shrink-0 text-text-secondary mono"
                style={{ width: 64 }}
              >
                {s.label}
              </span>
              <div className="flex-1 h-1.5 rounded-full bg-bg-primary overflow-hidden relative">
                <div
                  className={`h-full opacity-70 ${
                    overWindow ? "bg-error" : "bg-brand-primary"
                  }`}
                  style={{
                    width: `${Math.min(100, (s.ms / baseline) * 100)}%`,
                  }}
                />
              </div>
              <span
                className="num text-text-primary shrink-0 text-right"
                style={{ width: 64 }}
              >
                {s.ms.toFixed(1)} ms
              </span>
              <span
                className="num text-text-tertiary shrink-0 text-right"
                style={{ width: 44 }}
              >
                {s.pct.toFixed(0)}%
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
