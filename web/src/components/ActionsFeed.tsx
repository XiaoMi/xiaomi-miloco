/**
 * 「miloco 做了什么」动作审计流。
 *
 * 数据源:GET /api/actions(observability/router::list_actions)。
 * 返回 BARE JSON 数组(无 {code,data} 信封),新到旧排序。一次 agent 控制/播报/触发一行。
 * 行展示:时间 · 设备名(米家别名)+ 房间 · 动作类型 humanize + iid + value 截断 · 成功/失败徽标。
 * 交互:「只看失败」勾选走 failed_only=1 重拉;刷新按钮重拉。
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { apiFetch } from "@/api/client";

/** backend action_ledger 行——就地类型,不进 lib/types.ts(仅本组件用)。 */
export interface BackendActionRow {
  id: string;
  timestamp: number;
  action_type: string;
  did: string;
  device_name: string | null;
  room: string | null;
  iid: string | null;
  value_json: string | null;
  result_code: number | null;
  result_msg: string | null;
  success: 0 | 1;
  error: string | null;
  trace_id: string | null;
}

const VALUE_MAX = 60;

/** 统一拉取——failedOnly 时带 failed_only=1。导出供 tests 守 query 参数 + 解析。 */
export async function fetchActions(failedOnly: boolean): Promise<BackendActionRow[]> {
  const q = failedOnly ? "?limit=100&failed_only=1" : "?limit=100";
  return apiFetch<BackendActionRow[]>(`/api/actions${q}`);
}

/** ms → 本地 "MM-DD HH:mm:ss"(feed 用等宽时间列)。导出供 tests。 */
export function formatActionTime(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => (n < 10 ? `0${n}` : `${n}`);
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** action_type → i18n key。set_property/set_properties 归"设置属性";其余各自映射。 */
export function actionTypeKey(t: string): string {
  switch (t) {
    case "set_property":
    case "set_properties":
      return "actions.typeSetProperty";
    case "call_action":
      return "actions.typeCallAction";
    case "scene_trigger":
      return "actions.typeSceneTrigger";
    default:
      return "actions.typeUnknown";
  }
}

/** value_json 截断到 ~60 字符,超长加省略号(完整值走 title attr)。 */
function truncateValue(v: string | null): string {
  if (!v) return "";
  return v.length <= VALUE_MAX ? v : `${v.slice(0, VALUE_MAX)}…`;
}

export function ActionsFeed() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<BackendActionRow[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failedOnly, setFailedOnly] = useState(false);

  const load = useCallback((only: boolean) => {
    setLoading(true);
    setError(null);
    fetchActions(only)
      .then((data) => {
        setRows(data);
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
        setRows(null);
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load(failedOnly);
  }, [load, failedOnly]);

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm anim-in"
      aria-labelledby="actions-title"
    >
      <div className="flex items-baseline justify-between gap-3 px-5 pt-4 pb-3 flex-wrap">
        <h2
          id="actions-title"
          className="text-title text-text-primary inline-flex items-baseline gap-2"
        >
          {t("actions.title")}
          {rows && (
            <span className="text-caption-mono text-text-tertiary font-normal">
              {t("actions.loadedCount", { n: rows.length })}
            </span>
          )}
        </h2>
        <div className="inline-flex items-center gap-3">
          <label className="inline-flex items-center gap-1.5 text-caption text-text-secondary cursor-pointer select-none">
            <input
              type="checkbox"
              checked={failedOnly}
              onChange={(e) => setFailedOnly(e.target.checked)}
              className="accent-brand-primary w-[13px] h-[13px]"
            />
            {t("actions.failedOnly")}
          </label>
          <button
            type="button"
            onClick={() => load(failedOnly)}
            disabled={loading}
            className="text-caption px-3 py-1 rounded-md border border-border text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors disabled:opacity-50"
          >
            {t("actions.refresh")}
          </button>
        </div>
      </div>

      {loading && !rows ? (
        <div className="text-body text-center py-10 text-text-secondary">
          {t("actions.loading")}
        </div>
      ) : error ? (
        <div className="px-5 py-10 text-center">
          <div className="text-body text-error mb-3">{error}</div>
          <button
            type="button"
            onClick={() => load(failedOnly)}
            className="text-caption px-4 py-2 rounded-lg bg-bg-primary border border-border text-text-secondary hover:text-text-primary"
          >
            {t("actions.retry")}
          </button>
        </div>
      ) : !rows || rows.length === 0 ? (
        <div className="text-body text-center py-10 text-text-secondary">
          {t("actions.empty")}
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {rows.map((r) => (
            <ActionRow key={r.id} row={r} t={t} />
          ))}
        </ul>
      )}
    </section>
  );
}

function ActionRow({ row, t }: { row: BackendActionRow; t: TFunction }) {
  const ok = row.success === 1;
  const value = truncateValue(row.value_json);
  // 失败原因:优先 result_msg,退回 error;成功时不显。
  const reason = !ok ? row.result_msg || row.error || "" : "";
  const deviceLabel = row.device_name || row.did;

  return (
    <li className="px-5 py-2.5 hover:bg-bg-tertiary transition-colors">
      <div className="flex flex-col gap-1 sm:grid sm:grid-cols-[128px_1fr_auto] sm:gap-x-3 sm:gap-y-1 sm:items-baseline">
        <span className="text-caption-mono text-text-tertiary whitespace-nowrap">
          {formatActionTime(row.timestamp)}
        </span>

        <div className="min-w-0 sm:order-2">
          <div className="text-body text-text-primary break-words">
            <span className="font-medium">{deviceLabel}</span>
            {row.room && (
              <span className="text-caption text-text-tertiary ml-2">{row.room}</span>
            )}
          </div>
          <div className="text-caption text-text-secondary break-words">
            {t(actionTypeKey(row.action_type))}
            {row.iid && (
              <span className="text-caption-mono text-text-tertiary ml-1.5">{row.iid}</span>
            )}
            {value && (
              <span
                className="text-caption-mono text-text-tertiary ml-1.5 break-all"
                title={row.value_json ?? undefined}
              >
                {value}
              </span>
            )}
          </div>
          {reason && (
            <div className="text-caption text-error break-words mt-0.5" title={reason}>
              {reason}
            </div>
          )}
        </div>

        <span
          className={`text-caption px-2 py-0.5 rounded-full whitespace-nowrap sm:order-last sm:justify-self-end ${
            ok
              ? "text-success bg-success-bg"
              : "text-error bg-error-bg"
          }`}
        >
          {ok ? t("actions.resultSuccess") : t("actions.resultFailed")}
        </span>
      </div>
    </li>
  );
}
